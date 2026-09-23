"""Immutable oracle for juniper-green:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-juniper-green', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
