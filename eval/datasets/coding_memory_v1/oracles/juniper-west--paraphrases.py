"""Immutable oracle for juniper-west:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-juniper-west', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
