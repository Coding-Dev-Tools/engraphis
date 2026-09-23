"""Immutable oracle for grove-west:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-grove-west', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
