"""Immutable oracle for juniper-violet:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-juniper-violet', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
